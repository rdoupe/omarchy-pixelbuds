import QtQuick
import QtQml.Models
import Quickshell
import Quickshell.Bluetooth
import Quickshell.Io
import "Model.js" as Model

// One instance per shell (manifest kind "service"), shared by the bar widget
// on every monitor, so there is exactly one bridge and one Maestro session
// no matter how many bars show the widget.
//
// Connect/disconnect events come from the shell's own BlueZ client
// (Quickshell.Bluetooth), the same one Omarchy's Bluetooth panel uses, so the
// plugin parses no BlueZ output of its own.
//
// The bridge (bridge/pixelbuds_bridge.py) is started only on BlueZ connect
// events, holds a single RFCOMM session while the buds stay connected and
// exits on its own when they leave. Commands go to it as JSON lines on
// stdin; state comes back as bounded JSON lines on stdout. No shell is ever
// spawned, every launch is an absolute path with an argv array and a closed,
// allow-listed environment.
Item {
  id: root

  property var shell: null
  property var manifest: null

  visible: false
  implicitWidth: 0
  implicitHeight: 0

  // Distro identities. Never ambient PATH.
  readonly property string python3Bin: "/usr/bin/python3"
  readonly property string trustedPath: "/usr/bin:/bin"
  readonly property string bridgePath: Model.localPath(Qt.resolvedUrl("bridge/pixelbuds_bridge.py"))

  // Device state, already sanitized (Model.sanitizeStatus / sanitizeControls).
  property var status: ({})
  property var controls: ({})
  property string pendingAnc: ""
  property bool ready: false
  property bool readingControls: false
  readonly property bool connected: String(status.connected || "0") === "1"
  readonly property bool adaptiveSupported: String(status.adaptive_supported || "0") === "1"
  readonly property var ancModes: adaptiveSupported ? Model.ANC_MODES : Model.LEGACY_ANC_MODES

  property string _buf: ""
  property int _nextId: 1
  property bool _stopping: false
  property string _byeReason: ""
  property int _failures: 0
  property double _lastRefresh: 0

  // Allowlist only. clearEnvironment drops PYTHON*, LD_* and ambient PATH
  // before /usr/bin/python3 starts; the bridge needs only the system bus
  // address (if non-default) and the XDG dirs for its lock and cache.
  function launchEnvironment() {
    var env = {
      PATH: root.trustedPath,
      HOME: Quickshell.env("HOME") || "",
      LANG: Quickshell.env("LANG") || "C.UTF-8"
    }
    var pass = ["USER", "LOGNAME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
                "DBUS_SYSTEM_BUS_ADDRESS", "LC_ALL", "LC_CTYPE"]
    for (var i = 0; i < pass.length; i++) {
      var value = Quickshell.env(pass[i])
      if (value !== undefined && value !== null && String(value) !== "")
        env[pass[i]] = String(value)
    }
    return env
  }

  // ---- bridge lifecycle ----

  function ensureBridge() {
    if (bridge.running || _stopping) return
    restartTimer.stop()
    _buf = ""
    _byeReason = ""
    ready = false
    bridge.environment = root.launchEnvironment()
    bridge.running = true
  }

  // User disconnect or protocol violation: stop talking to the buds now.
  // The bridge shuts its RFCOMM socket on SIGTERM; SIGKILL follows if it
  // has not exited within 1.5 s.
  function stopBridge() {
    restartTimer.stop()
    ready = false
    readingControls = false
    pendingAnc = ""
    if (bridge.running) {
      _stopping = true
      bridge.running = false
      killTimer.restart()
    }
  }

  function abort() {
    stopBridge()
    status = ({})
    controls = ({})
  }

  function onBridgeExit(code) {
    killTimer.stop()
    var stopped = _stopping
    _stopping = false
    ready = false
    readingControls = false
    pendingAnc = ""
    if (stopped) return
    var reason = _byeReason !== "" ? _byeReason : (code === 0 ? "stdin_closed" : "crashed")
    if (Model.retryable(reason) && connected) {
      _failures++
      if (_failures <= 5) {
        status = Object.assign({}, status, { error: "Reconnecting to the buds…" })
        restartTimer.interval = Model.retryDelay(_failures)
        restartTimer.restart()
        return
      }
      status = Object.assign({}, status, { error: "Could not reach the buds' control service." })
      return
    }
    // absent / not_ready / disconnected: wait for the next BlueZ event.
    status = ({})
    controls = ({})
  }

  function onChunk(chunk) {
    var split = Model.splitLines(_buf, chunk)
    _buf = split.rest
    for (var i = 0; i < split.lines.length; i++) {
      if (!handleLine(split.lines[i])) { split.overflow = true; break }
    }
    if (split.overflow) {
      // Protocol violation: stop the session and drop whatever it said.
      _buf = ""
      abort()
    }
  }

  function handleLine(line) {
    if (line === "") return true
    var ev = Model.parseEvent(line)
    if (ev === null) return false
    switch (ev.type) {
      case "hello":
        return ev.v === Model.PROTOCOL_VERSION
      case "state":
        status = Model.sanitizeStatus(ev.status)
        if (pendingAnc !== "" && status.anc === pendingAnc) pendingAnc = ""
        return true
      case "controls":
        controls = Model.sanitizeControls(ev.controls)
        return true
      case "ready":
        ready = true
        _failures = 0
        return true
      case "result":
        if (ev.cmd === "set_anc" || ev.cmd === "cycle_anc") { if (ev.ok !== true) pendingAnc = "" }
        if (ev.cmd === "controls") readingControls = false
        return true
      case "bye":
        _byeReason = typeof ev.reason === "string" ? ev.reason.substring(0, 32) : "error"
        return true
      case "error":
        return true
      default:
        return true
    }
  }

  function send(cmd) {
    if (!bridge.running || !ready) return false
    cmd.id = _nextId
    _nextId = _nextId >= 2147483646 ? 1 : _nextId + 1
    bridge.write(JSON.stringify(cmd) + "\n")
    return true
  }

  // ---- API used by Panel.qml ----

  function refresh() {
    if (!bridge.running) {
      // Respect the retry limit: after five failed sessions only a new
      // BlueZ connect event (which resets _failures) starts the bridge.
      if (connected && !restartTimer.running && _failures <= 5) ensureBridge()
      return
    }
    var now = Date.now()
    if (now - _lastRefresh < 2000) return    // several bars poll; one read is enough
    if (send({ cmd: "refresh" })) _lastRefresh = now
  }

  function refreshControls() {
    if (send({ cmd: "controls" })) readingControls = true
  }

  function setAnc(mode) {
    if (ancModes.indexOf(mode) < 0) return
    if (send({ cmd: "set_anc", mode: mode })) pendingAnc = mode
  }

  function cycleAnc(delta) {
    // The buds cycle their own configured hold-gesture loop, so a click and
    // a physical long press always agree.
    pendingAnc = ""
    send({ cmd: "cycle_anc", direction: delta < 0 ? "prev" : "next" })
  }

  // key is one of the bridge's setting names; fields carry typed values,
  // e.g. { value: true }, { left: "anc", right: "assistant" },
  // { modes: ["off", "aware"] }, { value: -20 }, { bands: [0, 0, 0, 0, 0] }.
  function setControl(key, fields) {
    var cmd = { cmd: "set", key: String(key) }
    for (var k in fields) cmd[k] = fields[k]
    send(cmd)
  }

  // ---- processes ----

  Process {
    id: bridge
    command: [root.python3Bin, "-I", "-B", root.bridgePath]
    clearEnvironment: true
    environment: ({})
    stdinEnabled: true
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(chunk) { root.onChunk(chunk) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {}
    }
    onExited: function(code) { root.onBridgeExit(code) }
  }

  Timer {
    id: killTimer
    interval: 1500
    onTriggered: if (bridge.running) bridge.signal(9)
  }

  Timer {
    id: restartTimer
    interval: 1000
    onTriggered: root.ensureBridge()
  }

  // Connect/disconnect is event-driven: one watcher per BlueZ device known
  // to the shell. A device going down that is ours (or when we cannot tell)
  // stops the session at once, so nothing of ours holds a leaving link open;
  // the bridge also watches its own device on the bus.
  function onDeviceEvent(address, up) {
    if (up) {
      _failures = 0
    } else if (!status.addr || address === status.addr) {
      abort()
      _failures = 0
    }
    eventDebounce.restart()
  }

  Instantiator {
    model: Bluetooth.devices
    delegate: QtObject {
      required property var modelData
      readonly property bool linked: modelData.connected
      readonly property bool leaving: modelData.state === BluetoothDeviceState.Disconnecting
      onLinkedChanged: root.onDeviceEvent(modelData.address, linked)
      onLeavingChanged: if (leaving) root.onDeviceEvent(modelData.address, false)
    }
  }

  Timer {
    id: eventDebounce
    interval: 400
    onTriggered: root.ensureBridge()
  }

  Component.onCompleted: ensureBridge()

  Component.onDestruction: {
    restartTimer.stop()
    if (bridge.running) bridge.running = false
  }
}
