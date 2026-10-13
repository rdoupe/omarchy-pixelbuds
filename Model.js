.pragma library

// Bridge protocol constants (bridge/pixelbuds_bridge.py).
var PROTOCOL_VERSION = 1
var MAX_LINE = 4096          // bytes per bridge event line, enforced here too

var ANC_MODES = ["off", "active", "aware", "adaptive"]
var LEGACY_ANC_MODES = ["off", "active", "aware"]
var BOOL_CONTROLS = ["multipoint", "ohd", "speech-detection", "volume-exposure-notifications",
                     "volume-eq", "mono", "gestures"]

// file:///a%20b/x -> /a b/x
function localPath(url) {
  return decodeURIComponent(String(url || "").replace(/^file:\/\//, ""))
}

// Splits a stdout chunk stream into lines with a hard ceiling. Returns
// { lines: [...], rest: "...", overflow: bool }; on overflow the caller
// stops the bridge instead of buffering further.
function splitLines(buffer, chunk) {
  var all = String(buffer || "") + String(chunk || "")
  var lines = []
  var at = all.indexOf("\n")
  while (at !== -1) {
    var line = all.substring(0, at)
    if (line.length >= MAX_LINE) return { lines: lines, rest: "", overflow: true }
    lines.push(line)
    all = all.substring(at + 1)
    at = all.indexOf("\n")
  }
  if (all.length >= MAX_LINE) return { lines: lines, rest: "", overflow: true }
  return { lines: lines, rest: all, overflow: false }
}

function parseEvent(line) {
  // The bridge writes ASCII-only JSON; anything else is a protocol error.
  if (!/^[\x20-\x7e]*$/.test(line)) return null
  try {
    var obj = JSON.parse(line)
    return obj && typeof obj === "object" && !Array.isArray(obj) && typeof obj.type === "string" ? obj : null
  } catch (e) {
    return null
  }
}

function intIn(v, lo, hi) {
  return typeof v === "number" && isFinite(v) && Math.floor(v) === v && v >= lo && v <= hi
}

function shortString(v, max) {
  return typeof v === "string" && v.length <= max
}

// Only known keys with validated values survive; the bridge validates the
// same shapes, this is the second ceiling inside the long-lived shell.
function sanitizeStatus(raw) {
  var out = {}
  if (!raw || typeof raw !== "object") return out
  if (raw.connected === "1" || raw.connected === "0") out.connected = raw.connected
  if (typeof raw.addr === "string" && /^([0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(raw.addr)) out.addr = raw.addr
  if (shortString(raw.name, 100)) out.name = raw.name
  if (raw.adaptive_supported === "1" || raw.adaptive_supported === "0") out.adaptive_supported = raw.adaptive_supported
  out.anc = ANC_MODES.indexOf(raw.anc) >= 0 ? raw.anc : "unknown"
  var keys = ["left", "right", "case", "case_last"]
  for (var i = 0; i < keys.length; i++) {
    if (intIn(raw[keys[i]], -1, 100)) out[keys[i]] = raw[keys[i]]
    var st = raw[keys[i] + "_state"]
    if (st === "charging" || st === "not charging" || st === "unknown") out[keys[i] + "_state"] = st
  }
  if (intIn(raw.left_in_case, 0, 1)) out.left_in_case = raw.left_in_case
  if (intIn(raw.right_in_case, 0, 1)) out.right_in_case = raw.right_in_case
  if (intIn(raw.case_last_age, 0, 10000000000)) out.case_last_age = raw.case_last_age
  if (shortString(raw.error, 200) && raw.error !== "") out.error = raw.error
  return out
}

function sanitizeControls(raw) {
  var out = {}
  if (!raw || typeof raw !== "object") return out
  for (var i = 0; i < BOOL_CONTROLS.length; i++) {
    var k = "ctl_" + BOOL_CONTROLS[i].replace(/-/g, "_")
    if (raw[k] === "true" || raw[k] === "false") out[k] = raw[k]
  }
  if (raw.ctl_gesture_left === "anc" || raw.ctl_gesture_left === "assistant") out.ctl_gesture_left = raw.ctl_gesture_left
  if (raw.ctl_gesture_right === "anc" || raw.ctl_gesture_right === "assistant") out.ctl_gesture_right = raw.ctl_gesture_right
  if (typeof raw.ctl_anc_gesture_loop === "string"
      && /^(off|active|aware|adaptive)(,(off|active|aware|adaptive)){0,3}$/.test(raw.ctl_anc_gesture_loop))
    out.ctl_anc_gesture_loop = raw.ctl_anc_gesture_loop
  if (intIn(raw.ctl_balance, -100, 100)) out.ctl_balance = raw.ctl_balance
  if (typeof raw.ctl_eq === "string"
      && /^-?[0-9]{1,2}\.[0-9]{2}(,-?[0-9]{1,2}\.[0-9]{2}){4}$/.test(raw.ctl_eq))
    out.ctl_eq = raw.ctl_eq
  return out
}

// Bridge exit reasons after which a new session is worth trying while the
// buds still look connected. Everything else waits for the next BlueZ event.
function retryable(reason) {
  return ["link_lost", "connect_failed", "busy", "error", "crashed"].indexOf(String(reason)) >= 0
}

function retryDelay(failures) {
  var delays = [1000, 3000, 10000, 30000, 60000]
  return delays[Math.max(0, Math.min(delays.length - 1, failures - 1))]
}

function ancLabel(mode) {
  switch (String(mode || "")) {
    case "off": return "Off"
    case "active": return "Noise Cancelling"
    case "aware": return "Transparency"
    case "adaptive": return "Adaptive"
    default: return "Unknown"
  }
}

function ancShort(mode) {
  switch (String(mode || "")) {
    case "off": return "Off"
    case "active": return "ANC"
    case "aware": return "Aware"
    case "adaptive": return "Adaptive"
    default: return "?"
  }
}

function ancIcon(mode) {
  switch (String(mode || "")) {
    case "off": return "󰟎"      // headphones-off
    case "active": return "󰋋"   // headphones
    case "aware": return "󰕾"    // volume-high
    case "adaptive": return "󰁨" // auto-fix
    default: return "󰋋"
  }
}

function ancIndex(mode) {
  var i = ANC_MODES.indexOf(String(mode || ""))
  return i < 0 ? 1 : i
}

function pct(status, key) {
  var v = parseInt(status[key])
  return isNaN(v) ? -1 : v
}

function charging(status, key) {
  return String(status[key + "_state"] || "") === "charging"
}

// Lowest known bud level; -1 when neither bud reported.
function budsMin(status) {
  var l = pct(status, "left"), r = pct(status, "right")
  if (l < 0) return r
  if (r < 0) return l
  return Math.min(l, r)
}

// "just now", "12m ago", "3h ago", "2d ago"
function ageText(sec) {
  if (sec < 90) return "just now"
  if (sec < 5400) return Math.round(sec / 60) + "m ago"
  if (sec < 129600) return Math.round(sec / 3600) + "h ago"
  return Math.round(sec / 86400) + "d ago"
}

function batteryIcon(level, isCharging) {
  if (level < 0) return "󰂑"
  if (isCharging) return "󰂄"
  var icons = ["󰂎", "󰁺", "󰁻", "󰁼", "󰁽", "󰁾", "󰁿", "󰂀", "󰂁", "󰂂", "󰁹"]
  return icons[Math.max(0, Math.min(10, Math.round(level / 10)))]
}

function volumeIcon(percent) {
  if (percent >= 67) return ""
  if (percent >= 34) return ""
  if (percent > 0) return ""
  return ""
}
