.pragma library

// Parses status.sh output ("key=value" per line) into an object.
function parseStatus(raw) {
  var out = {}
  var lines = String(raw || "").split("\n")
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (line === "") continue
    var eq = line.indexOf("=")
    if (eq <= 0) continue
    out[line.substring(0, eq)] = line.substring(eq + 1)
  }
  return out
}

var ANC_MODES = ["off", "active", "aware", "adaptive"]

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
