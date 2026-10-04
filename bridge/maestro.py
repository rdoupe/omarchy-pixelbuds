# SPDX-License-Identifier: MIT
#
# Maestro protocol codec for Google Pixel Buds (HDLC framing, pw_rpc packets,
# protobuf field codec, Maestro settings).
#
# This file is a Python port of parts of qzed/pbpctrl's `libmaestro` crate
# (https://github.com/qzed/pbpctrl), Copyright (c) 2022 Maximilian Luz,
# dual-licensed MIT OR Apache-2.0 upstream and used here under the MIT
# License. Ported: HDLC frame encoding/decoding and the 65599 id hash
# (src/hdlc, src/pwrpc/id.rs), the Maestro peer/channel address map
# (src/protocol/addr.rs), the Maestro message field numbers
# (proto/maestro_pw.proto) and the setting conversions
# (src/service/settings.rs). The pw_rpc packet field numbers follow the wire
# format defined by the Pigweed project (pw_rpc); no Pigweed code is
# included. Changes: rewritten in Python, every decoder made byte-bounded and
# strict for untrusted device input, blocking RPC client written for this
# project. See NOTICE for the full attribution.
"""Pure, I/O-free Maestro protocol pieces plus a transport-agnostic RPC client.

Everything that parses bytes received from the buds treats them as hostile:
frame sizes, varint lengths, field counts and nesting are all bounded, and a
malformed frame is dropped rather than raising into the caller.
"""
from __future__ import annotations

import math
import struct
import time
import zlib

MAESTRO_UUID = "25e97ff7-24ce-4c4c-8951-f764a708f7b5"

# --------------------------------------------------------------------------
# HDLC framing (pbpctrl src/hdlc)
# --------------------------------------------------------------------------

FLAG = 0x7E
ESCAPE = 0x7D
ESCAPE_MASK = 0x20
CONTROL_UI = 0x03          # unnumbered information frame, the only kind used
MAX_FRAME = 4096           # decoded bytes per frame (pbpctrl uses the same capacity)


def crc32(data):
    """CRC-32/ISO-HDLC, identical to pbpctrl's table implementation."""
    return zlib.crc32(bytes(data)) & 0xFFFFFFFF


def encode_address(value):
    """HDLC variable-length address: 7 bits per byte, LSB set on the last."""
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("address out of range")
    out = bytearray()
    while True:
        if value >> 7:
            out.append((value & 0x7F) << 1)
            value >>= 7
        else:
            out.append(((value & 0x7F) << 1) | 1)
            return bytes(out)


def decode_address(data):
    """Return (address, bytes_used). Raises ValueError when incomplete/overflowing."""
    value = 0
    for i, b in enumerate(data):
        if i >= 5:
            break
        value |= (b >> 1) << (i * 7)
        if value > 0xFFFFFFFF:
            raise ValueError("address overflow")
        if b & 1:
            return value, i + 1
    raise ValueError("address incomplete")


def _escape_into(out, data):
    for b in data:
        if b in (FLAG, ESCAPE):
            out.append(ESCAPE)
            out.append(b ^ ESCAPE_MASK)
        else:
            out.append(b)


def hdlc_encode(address, data, control=CONTROL_UI):
    body = encode_address(address) + bytes([control]) + bytes(data)
    body += struct.pack("<I", crc32(body))
    out = bytearray([FLAG])
    _escape_into(out, body)
    out.append(FLAG)
    return bytes(out)


class Frame:
    __slots__ = ("address", "control", "data")

    def __init__(self, address, control, data):
        self.address = address
        self.control = control
        self.data = data

    def __eq__(self, other):
        return (isinstance(other, Frame) and self.address == other.address
                and self.control == other.control and self.data == other.data)

    def __repr__(self):
        return "Frame(0x%x, 0x%02x, %r)" % (self.address, self.control, self.data)


class HdlcDecoder:
    """Incremental, bounded HDLC decoder.

    Bytes outside a frame are discarded; an escape error, a frame over
    MAX_FRAME decoded bytes, a short frame or a bad CRC drops that frame and
    resynchronises on the next flag. Never buffers more than MAX_FRAME bytes.
    """

    def __init__(self, max_frame=MAX_FRAME):
        self.max_frame = max_frame
        self.buf = bytearray()
        self.in_frame = False
        self.escaped = False
        self.overflow = False
        self.errors = 0

    def _reset(self):
        self.buf.clear()
        self.escaped = False
        self.overflow = False

    def feed(self, chunk):
        frames = []
        for b in bytes(chunk):
            if b == FLAG:
                if self.in_frame and (self.buf or self.escaped or self.overflow):
                    frame = self._finish()
                    if frame is not None:
                        frames.append(frame)
                # A flag both ends one frame and may start the next.
                self.in_frame = True
                self._reset()
                continue
            if not self.in_frame:
                continue
            if self.overflow:
                continue
            if b == ESCAPE:
                if self.escaped:            # double escape: invalid encoding
                    self.errors += 1
                    self.in_frame = False
                    self._reset()
                    continue
                self.escaped = True
                continue
            if self.escaped:
                b ^= ESCAPE_MASK
                self.escaped = False
            if len(self.buf) >= self.max_frame:
                self.overflow = True        # drop the rest of this frame
                self.buf.clear()
                continue
            self.buf.append(b)
        return frames

    def _finish(self):
        if self.escaped or self.overflow:
            self.errors += 1
            return None
        raw = bytes(self.buf)
        if len(raw) < 6:
            self.errors += 1
            return None
        if crc32(raw[:-4]) != struct.unpack("<I", raw[-4:])[0]:
            self.errors += 1
            return None
        try:
            address, n = decode_address(raw[:-4])
        except ValueError:
            self.errors += 1
            return None
        if len(raw) < n + 5:
            self.errors += 1
            return None
        return Frame(address, raw[n], raw[n + 1:-4])


# --------------------------------------------------------------------------
# Maestro addressing (pbpctrl src/protocol/addr.rs)
# --------------------------------------------------------------------------

PEER_CASE, PEER_LEFT_BT, PEER_RIGHT_BT = 2, 3, 4
PEER_LEFT_SH, PEER_RIGHT_SH = 5, 6
PEER_MAESTRO_A, PEER_MAESTRO_B = 10, 13

CHANNELS = {
    18: (PEER_MAESTRO_A, PEER_CASE),
    19: (PEER_MAESTRO_A, PEER_LEFT_BT),
    20: (PEER_MAESTRO_A, PEER_LEFT_SH),
    21: (PEER_MAESTRO_A, PEER_RIGHT_BT),
    22: (PEER_MAESTRO_A, PEER_RIGHT_SH),
    23: (PEER_MAESTRO_B, PEER_CASE),
    24: (PEER_MAESTRO_B, PEER_LEFT_BT),
    25: (PEER_MAESTRO_B, PEER_LEFT_SH),
    26: (PEER_MAESTRO_B, PEER_RIGHT_BT),
    27: (PEER_MAESTRO_B, PEER_RIGHT_SH),
}
# Channels probed during resolution, in pbpctrl's order.
RESOLVE_CHANNELS = (18, 19, 21, 23, 24, 26)


def address_from_peers(source, target):
    return ((source & 0xF) << 6) | ((target & 0xF) << 10)


def address_for_channel(channel):
    peers = CHANNELS.get(channel)
    if peers is None:
        raise ValueError("unknown Maestro channel %r" % (channel,))
    return address_from_peers(*peers)


# --------------------------------------------------------------------------
# pw_rpc ids (pbpctrl src/pwrpc/id.rs)
# --------------------------------------------------------------------------

def hash65599(name):
    h = len(name) & 0xFFFFFFFF
    coef = 65599
    for ch in name:
        h = (h + coef * ord(ch)) & 0xFFFFFFFF
        coef = (coef * 65599) & 0xFFFFFFFF
    return h


def path_ids(path):
    service, _, method = path.rpartition("/")
    return hash65599(service), hash65599(method)


SVC_MAESTRO = "maestro_pw.Maestro"
M_GET_SOFTWARE_INFO = SVC_MAESTRO + "/GetSoftwareInfo"
M_SUB_RUNTIME_INFO = SVC_MAESTRO + "/SubscribeRuntimeInfo"
M_WRITE_SETTING = SVC_MAESTRO + "/WriteSetting"
M_READ_SETTING = SVC_MAESTRO + "/ReadSetting"
M_SUB_SETTINGS = SVC_MAESTRO + "/SubscribeToSettingsChanges"

# --------------------------------------------------------------------------
# Protobuf wire codec (bounded)
# --------------------------------------------------------------------------

WT_VARINT, WT_I64, WT_LEN, WT_I32 = 0, 1, 2, 5
MAX_FIELDS = 64            # per message; Maestro messages carry a handful


class DecodeError(ValueError):
    pass


def encode_varint(value):
    value = int(value)
    if value < 0:
        value &= (1 << 64) - 1      # proto int32/int64 negative encoding
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint(data, pos):
    value = 0
    for i in range(10):
        if pos >= len(data):
            raise DecodeError("truncated varint")
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            if value >= 1 << 64:
                raise DecodeError("varint overflow")
            return value, pos
    raise DecodeError("varint too long")


def key(field, wire):
    return encode_varint((field << 3) | wire)


def f_varint(field, value):
    return key(field, WT_VARINT) + encode_varint(value)


def f_bytes(field, value):
    value = bytes(value)
    return key(field, WT_LEN) + encode_varint(len(value)) + value


def f_fixed32(field, value):
    return key(field, WT_I32) + struct.pack("<I", value & 0xFFFFFFFF)


def f_float(field, value):
    return key(field, WT_I32) + struct.pack("<f", value)


def parse_fields(data):
    """Return {field: [(wire, value), ...]} for one message level.

    LEN values are returned as raw bytes; nested parsing is explicit, so
    recursion depth is bounded by the caller's schema.
    """
    data = bytes(data)
    fields = {}
    pos = count = 0
    while pos < len(data):
        count += 1
        if count > MAX_FIELDS:
            raise DecodeError("too many fields")
        k, pos = decode_varint(data, pos)
        field, wire = k >> 3, k & 7
        if field == 0 or field > 0x1FFFFFFF:
            raise DecodeError("bad field number")
        if wire == WT_VARINT:
            value, pos = decode_varint(data, pos)
        elif wire == WT_I64:
            if pos + 8 > len(data):
                raise DecodeError("truncated fixed64")
            value = data[pos:pos + 8]
            pos += 8
        elif wire == WT_LEN:
            n, pos = decode_varint(data, pos)
            if n > len(data) - pos:
                raise DecodeError("truncated length-delimited field")
            value = data[pos:pos + n]
            pos += n
        elif wire == WT_I32:
            if pos + 4 > len(data):
                raise DecodeError("truncated fixed32")
            value = data[pos:pos + 4]
            pos += 4
        else:
            raise DecodeError("unsupported wire type %d" % wire)
        fields.setdefault(field, []).append((wire, value))
    return fields


def _last(fields, field, wire):
    # proto3: the last occurrence of a scalar wins.
    for w, v in reversed(fields.get(field, ())):
        if w == wire:
            return v
    return None


def get_uint(fields, field, default=None):
    v = _last(fields, field, WT_VARINT)
    return default if v is None else v


def get_int32(fields, field, default=None):
    v = _last(fields, field, WT_VARINT)
    if v is None:
        return default
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def get_bool(fields, field, default=None):
    v = _last(fields, field, WT_VARINT)
    return default if v is None else bool(v)


def get_fixed32(fields, field, default=None):
    v = _last(fields, field, WT_I32)
    return default if v is None else struct.unpack("<I", v)[0]


def get_float(fields, field, default=None):
    v = _last(fields, field, WT_I32)
    return default if v is None else struct.unpack("<f", v)[0]


def get_bytes(fields, field, default=None):
    v = _last(fields, field, WT_LEN)
    return default if v is None else v


def get_message(fields, field):
    v = _last(fields, field, WT_LEN)
    return None if v is None else parse_fields(v)


# --------------------------------------------------------------------------
# pw_rpc packet (pw.rpc.packet.proto)
# --------------------------------------------------------------------------

PT_REQUEST, PT_RESPONSE, PT_CLIENT_ERROR = 0, 1, 4
PT_SERVER_ERROR, PT_SERVER_STREAM = 5, 7
STATUS_OK, STATUS_CANCELLED, STATUS_FAILED_PRECONDITION = 0, 1, 9
OPEN_CALL_ID = 0xFFFFFFFF


class RpcPacket:
    __slots__ = ("type", "channel_id", "service_id", "method_id", "payload", "status", "call_id")

    def __init__(self, type=0, channel_id=0, service_id=0, method_id=0,
                 payload=b"", status=0, call_id=0):
        self.type = type
        self.channel_id = channel_id
        self.service_id = service_id
        self.method_id = method_id
        self.payload = bytes(payload)
        self.status = status
        self.call_id = call_id

    def encode(self):
        # prost/proto3 semantics: default scalars are omitted.
        out = bytearray()
        if self.type:
            out += f_varint(1, self.type)
        if self.channel_id:
            out += f_varint(2, self.channel_id)
        if self.service_id:
            out += f_fixed32(3, self.service_id)
        if self.method_id:
            out += f_fixed32(4, self.method_id)
        if self.payload:
            out += f_bytes(5, self.payload)
        if self.status:
            out += f_varint(6, self.status)
        if self.call_id:
            out += f_varint(7, self.call_id)
        return bytes(out)

    @classmethod
    def decode(cls, data):
        f = parse_fields(data)
        return cls(
            type=get_uint(f, 1, 0) & 0xFFFFFFFF,
            channel_id=get_uint(f, 2, 0) & 0xFFFFFFFF,
            service_id=get_fixed32(f, 3, 0),
            method_id=get_fixed32(f, 4, 0),
            payload=get_bytes(f, 5, b""),
            status=get_uint(f, 6, 0) & 0xFFFFFFFF,
            call_id=get_uint(f, 7, 0) & 0xFFFFFFFF,
        )

    def uid(self):
        return (self.channel_id, self.service_id, self.method_id, self.call_id)

    def __repr__(self):
        return ("RpcPacket(type=%d, ch=%d, svc=0x%08x, m=0x%08x, call=0x%x, status=%d, %d bytes)"
                % (self.type, self.channel_id, self.service_id, self.method_id,
                   self.call_id, self.status, len(self.payload)))


def encode_rpc_frame(packet):
    return hdlc_encode(address_for_channel(packet.channel_id), packet.encode())


def decode_rpc_frame(frame):
    """RpcPacket from an HDLC frame, or None for anything that is not one."""
    if frame.control != CONTROL_UI:
        return None
    # Port 7 carries a transport poll on some firmware, not an RpcPacket.
    if frame.address & 0x7 == 7:
        return None
    try:
        return RpcPacket.decode(frame.data)
    except DecodeError:
        return None


# --------------------------------------------------------------------------
# Maestro settings (maestro_pw.proto + pbpctrl src/service/settings.rs)
# --------------------------------------------------------------------------

S_OHD = 2
S_GESTURES = 4
S_GESTURE_CONTROL = 7
S_MULTIPOINT = 11
S_ANC_GESTURE_LOOP = 12
S_ANC = 13
S_VOLUME_EQ = 15
S_EQ = 16
S_BALANCE = 17
S_MONO = 19
S_VOLUME_EXPOSURE = 21
S_SPEECH_DETECTION = 22

BOOL_SETTINGS = {
    "multipoint": S_MULTIPOINT,
    "ohd": S_OHD,
    "speech-detection": S_SPEECH_DETECTION,
    "volume-exposure-notifications": S_VOLUME_EXPOSURE,
    "volume-eq": S_VOLUME_EQ,
    "mono": S_MONO,
    "gestures": S_GESTURES,
}

ANC_BY_VALUE = {1: "off", 2: "active", 3: "aware", 4: "adaptive"}
ANC_VALUE = {v: k for k, v in ANC_BY_VALUE.items()}
# pbpctrl cmd_anc_cycle order.
ANC_CYCLE = ("active", "off", "aware", "adaptive")
# AncrGestureLoop field numbers.
LOOP_FIELDS = {"active": 1, "off": 2, "aware": 3, "adaptive": 4}

ACTION_ANC = 5
ACTION_ASSISTANT = 6
ACTION_BY_VALUE = {ACTION_ANC: "anc", ACTION_ASSISTANT: "assistant"}
ACTION_VALUE = {v: k for k, v in ACTION_BY_VALUE.items()}

EQ_MIN, EQ_MAX = -6.0, 6.0

BATTERY_STATE = {1: "not charging", 2: "charging"}


def balance_from_raw(raw):
    direction = raw & 1
    value = raw >> 1
    return value + 1 if direction else -value


def balance_to_raw(value):
    value = max(-100, min(100, int(value)))
    if value > 0:
        return ((value - 1) << 1) | 1
    return (-value) << 1


def read_setting_request(setting_id):
    return f_varint(4, setting_id)        # ReadSettingMsg.settings_id


def write_setting_request(setting_value_bytes):
    return f_bytes(4, setting_value_bytes)  # WriteSettingMsg.setting


def sv_bool(setting_id, value):
    return f_varint(setting_id, 1 if value else 0)


def sv_anc(mode):
    return f_varint(S_ANC, ANC_VALUE[mode])


def sv_gesture_control(left, right):
    def side(action):
        return f_bytes(4, f_varint(1, ACTION_VALUE[action]))
    return f_bytes(S_GESTURE_CONTROL, f_bytes(1, side(left)) + f_bytes(2, side(right)))


def sv_anc_gesture_loop(modes):
    body = bytearray()
    for name in ("active", "off", "aware", "adaptive"):
        if name in modes:
            body += f_varint(LOOP_FIELDS[name], 1)
    return f_bytes(S_ANC_GESTURE_LOOP, bytes(body))


def sv_balance(value):
    return f_varint(S_BALANCE, balance_to_raw(value))


def sv_eq(bands):
    body = bytearray()
    for i, v in enumerate(bands, start=1):
        v = max(EQ_MIN, min(EQ_MAX, float(v)))
        if v != 0.0:
            body += f_float(i, v)
    return f_bytes(S_EQ, bytes(body))


def decode_setting_value(payload):
    """Decode a SettingsRsp payload into (name, value) or None.

    Every value is validated against its type; anything else yields None.
    """
    rsp = parse_fields(payload)
    inner = get_bytes(rsp, 4)
    if inner is None:
        return None
    sv = parse_fields(inner)
    # SettingValue is a oneof: exactly one member must be present.
    if len(sv) != 1:
        return None
    field = next(iter(sv))
    for name, sid in BOOL_SETTINGS.items():
        if field == sid:
            v = _last(sv, sid, WT_VARINT)
            if v is None or v > 1:
                return None
            return (name, bool(v))
    if field == S_ANC:
        v = get_uint(sv, S_ANC)
        return ("anc", ANC_BY_VALUE.get(v, "unknown"))
    if field == S_GESTURE_CONTROL:
        gc = get_message(sv, S_GESTURE_CONTROL)
        if gc is None:
            return None
        out = {}
        for side, num in (("left", 1), ("right", 2)):
            dev = get_message(gc, num)
            typ = get_message(dev, 4) if dev is not None else None
            val = get_uint(typ, 1) if typ is not None else None
            out[side] = ACTION_BY_VALUE.get(val, "other")
        return ("gesture-control", out)
    if field == S_ANC_GESTURE_LOOP:
        loop = get_message(sv, S_ANC_GESTURE_LOOP)
        if loop is None:
            return None
        return ("anc-gesture-loop", [n for n in ("active", "off", "aware", "adaptive")
                                     if get_bool(loop, LOOP_FIELDS[n], False)])
    if field == S_BALANCE:
        raw = get_int32(sv, S_BALANCE)
        if raw is None or not 0 <= raw <= 200:
            return None
        return ("balance", balance_from_raw(raw))
    if field == S_EQ:
        eq = get_message(sv, S_EQ)
        if eq is None:
            return None
        bands = []
        for i in range(1, 6):
            v = get_float(eq, i, 0.0)
            if not math.isfinite(v) or not EQ_MIN - 0.01 <= v <= EQ_MAX + 0.01:
                return None
            bands.append(round(v, 2))
        return ("eq", bands)
    return ("unsupported", field)


def decode_runtime_info(payload):
    """RuntimeInfo -> dict with battery and placement; values validated."""
    rt = parse_fields(payload)
    out = {}
    bat = get_message(rt, 6)
    for name, num in (("case", 1), ("left", 2), ("right", 3)):
        dev = get_message(bat, num) if bat is not None else None
        if dev is None:
            out[name] = -1
            out[name + "_state"] = "unknown"
            continue
        level = get_int32(dev, 1, 0)
        out[name] = level if 0 <= level <= 100 else -1
        out[name + "_state"] = BATTERY_STATE.get(get_uint(dev, 2, 0), "unknown")
    place = get_message(rt, 7)
    if place is not None:
        out["right_in_case"] = 1 if get_bool(place, 1, False) else 0
        out["left_in_case"] = 1 if get_bool(place, 2, False) else 0
    return out


# --------------------------------------------------------------------------
# Blocking pw_rpc client over an abstract byte transport
# --------------------------------------------------------------------------

class RpcError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class RpcTimeout(RpcError):
    pass


class RpcClient:
    """Serialized pw_rpc client.

    `io` must provide:
      send(data)            write all bytes to the buds (bounded by caller)
      wait(deadline)        block until bytes arrive or the deadline passes;
                            returns a (possibly empty) bytes chunk; raises to
                            abort (stdin EOF, user disconnect, signal)
    Only one unary call is in flight at a time. Server-stream items are handed
    to `on_stream(method_path, payload)` as they arrive.
    """

    MAX_STREAMS = 4

    def __init__(self, io, on_stream=None, clock=time.monotonic):
        self.io = io
        self.on_stream = on_stream
        self.clock = clock
        self.decoder = HdlcDecoder()
        self.channel = None
        self.next_call_id = 1
        self.streams = {}          # uid -> method path
        self._pending = None       # (uid, method path)
        self._result = None
        self._unsolicited = []     # packets seen while not waiting for them

    def _alloc_call_id(self):
        cid = self.next_call_id
        self.next_call_id = cid + 1 if cid < 0x7FFFFFFF else 1
        return cid

    def _send(self, packet):
        self.io.send(encode_rpc_frame(packet))

    def _pump(self, deadline):
        chunk = self.io.wait(deadline)
        if not chunk:
            return
        for frame in self.decoder.feed(chunk):
            packet = decode_rpc_frame(frame)
            if packet is not None:
                self._dispatch(packet)

    def _dispatch(self, packet):
        uid = packet.uid()
        if self._pending is not None:
            puid = self._pending[0]
            # Exact match, or a firmware that answers with call id 0 for the
            # single call in flight on the same channel/service/method.
            if (uid == puid or (packet.call_id == 0 and uid[:3] == puid[:3])) and \
                    packet.type in (PT_RESPONSE, PT_SERVER_ERROR):
                self._result = packet
                return
        if uid in self.streams:
            if packet.type == PT_SERVER_STREAM:
                if self.on_stream is not None:
                    self.on_stream(self.streams[uid], packet.payload)
            elif packet.type in (PT_RESPONSE, PT_SERVER_ERROR):
                self.streams.pop(uid, None)
            return
        if len(self._unsolicited) < 16:
            self._unsolicited.append(packet)

    def resolve_channel(self, timeout=5.0, probe_timeout=1.5):
        """Find the Maestro channel, as pbpctrl does: the buds announce
        GetSoftwareInfo with call id 0xffffffff on their channel right after
        the RFCOMM link opens. Falls back to probing each channel."""
        svc, meth = path_ids(M_GET_SOFTWARE_INFO)
        deadline = self.clock() + timeout
        while True:
            for p in self._unsolicited:
                if (p.type == PT_RESPONSE and p.service_id == svc and p.method_id == meth
                        and p.channel_id in RESOLVE_CHANNELS and p.status == STATUS_OK):
                    self.channel = p.channel_id
                    self._unsolicited.clear()
                    return self.channel
            self._unsolicited.clear()
            if self.clock() >= deadline:
                break
            self._pump(deadline)
        for ch in RESOLVE_CHANNELS:
            try:
                self.call(M_GET_SOFTWARE_INFO, b"", timeout=probe_timeout, channel=ch)
            except RpcTimeout:
                continue
            except RpcError:
                continue
            self.channel = ch
            return ch
        raise RpcError("no Maestro channel answered")

    def call(self, method, payload=b"", timeout=3.0, channel=None):
        ch = self.channel if channel is None else channel
        if ch is None:
            raise RpcError("channel not resolved")
        svc, meth = path_ids(method)
        packet = RpcPacket(PT_REQUEST, ch, svc, meth, payload, 0, self._alloc_call_id())
        self._pending = (packet.uid(), method)
        self._result = None
        try:
            self._send(packet)
            deadline = self.clock() + timeout
            while self._result is None:
                if self.clock() >= deadline:
                    # Tell the server we gave up, so a late reply is not kept.
                    self._send(RpcPacket(PT_CLIENT_ERROR, ch, svc, meth, b"",
                                         STATUS_CANCELLED, packet.call_id))
                    raise RpcTimeout("%s timed out" % method)
                self._pump(deadline)
            result = self._result
        finally:
            self._pending = None
            self._result = None
        if result.type == PT_SERVER_ERROR or result.status != STATUS_OK:
            raise RpcError("%s failed with status %d" % (method, result.status), result.status)
        return result.payload

    def subscribe(self, method):
        if self.channel is None:
            raise RpcError("channel not resolved")
        for uid, m in list(self.streams.items()):
            if m == method:
                self.cancel(uid)
        if len(self.streams) >= self.MAX_STREAMS:
            raise RpcError("too many streams")
        svc, meth = path_ids(method)
        packet = RpcPacket(PT_REQUEST, self.channel, svc, meth, b"", 0, self._alloc_call_id())
        self.streams[packet.uid()] = method
        self._send(packet)
        return packet.uid()

    def cancel(self, uid):
        if self.streams.pop(uid, None) is not None:
            ch, svc, meth, cid = uid
            self._send(RpcPacket(PT_CLIENT_ERROR, ch, svc, meth, b"", STATUS_CANCELLED, cid))

    def poll(self, deadline):
        """Process incoming bytes (stream pushes) until the deadline."""
        self._pump(deadline)
