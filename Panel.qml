import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Pixel Buds Pro in the bar. Data comes from status.sh -> pbpctrl (RFCOMM to
// the buds); the only direct Bluetooth contact here is a read-only signal
// subscription on org.bluez that makes connect/disconnect show up instantly.
// Hidden entirely while no Pixel Buds are connected.
Panel {
  id: root
  moduleName: "douper.pixelbuds"
  ipcTarget: "douper.pixelbuds"
  manageIpc: false

  property var status: ({})
  property var controls: ({})
  property bool advancedOpen: false
  property bool everLoaded: false
  property int ancIndex: 1
  property bool cursorActive: false
  property string pendingAnc: ""

  readonly property string scriptPath: String(Qt.resolvedUrl("status.sh")).replace(/^file:\/\//, "")
  readonly property int pollInterval: Math.max(5, parseInt(setting("pollIntervalSec", 30)) || 30) * 1000
  readonly property bool hideWhenDisconnected: String(setting("hideWhenDisconnected", true)) === "true"
  readonly property color urgentColor: bar ? bar.urgent : Color.urgent
  readonly property var eqBands: controls.ctl_eq !== undefined ? String(controls.ctl_eq).split(",").map(Number) : []

  readonly property bool connected: String(status.connected || "0") === "1"
  readonly property bool missingPbpctrl: String(status.missing_pbpctrl || "0") === "1"
  readonly property string budsName: String(status.name || "Pixel Buds")
  readonly property string anc: pendingAnc !== "" ? pendingAnc : String(status.anc || "unknown")
  readonly property int leftPct: Model.pct(status, "left")
  readonly property int rightPct: Model.pct(status, "right")
  readonly property int casePct: Model.pct(status, "case")
  readonly property int caseLastPct: Model.pct(status, "case_last")
  readonly property int caseLastAge: Model.pct(status, "case_last_age")
  readonly property bool caseStale: casePct < 0 && caseLastPct >= 0
  readonly property bool leftCharging: Model.charging(status, "left")
  readonly property bool rightCharging: Model.charging(status, "right")
  readonly property bool caseCharging: Model.charging(status, "case")
  readonly property bool leftInCase: String(status.left_in_case || "0") === "1"
  readonly property bool rightInCase: String(status.right_in_case || "0") === "1"
  readonly property int minPct: Model.budsMin(status)
  readonly property bool anyCharging: leftCharging || rightCharging

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Only one pbpctrl process may talk to the buds at a time — concurrent
  // RFCOMM sessions make each other's reads fail — so everything below
  // funnels through this gate, and a controls request made while status is
  // being read runs right after it instead.
  property bool controlsQueued: false
  readonly property bool busy: statusProc.running || controlsProc.running
      || actionProc.running || ctlProc.running

  function refresh() {
    if (busy) return
    statusProc.running = true
  }

  function refreshControls() {
    if (!connected || missingPbpctrl || controlsProc.running) return
    if (busy) { controlsQueued = true; return }
    controlsProc.running = true
  }

  function applyControls(raw) {
    var next = Model.parseStatus(raw)
    var out = {}
    for (var k in next) if (k.indexOf("ctl_") === 0) out[k] = next[k]
    controls = out
    dispatchPending()
  }

  // addr is a bluetoothctl MAC, key a literal subcommand, args numbers/bools —
  // nothing here is user text, so plain string assembly is safe. The "--"
  // keeps negative values (balance, EQ) from parsing as flags. A set that
  // arrives while the RFCOMM link is busy waits its turn instead of failing;
  // last write wins.
  property var pendingCtl: null

  function setControl(key, args) {
    if (!connected || missingPbpctrl) return
    if (busy) { pendingCtl = [key, args]; return }
    runCtl(key, args)
  }

  function runCtl(key, args) {
    ctlProc.command = ["sh", "-c",
      "exec timeout 6 pbpctrl -d " + String(status.addr || "") + " set " + key + " -- " + args]
    ctlProc.running = true
  }

  function dispatchPending() {
    if (pendingCtl === null || busy) return
    var p = pendingCtl
    pendingCtl = null
    runCtl(p[0], p[1])
  }

  function setEqBand(i, v) {
    var b = eqBands.slice()
    if (b.length !== 5) return
    b[i] = v
    setControl("eq", b.map(function(x) { return Number(x).toFixed(1) }).join(" "))
  }

  function applyStatus(raw) {
    var next = Model.parseStatus(raw)
    if (Object.keys(next).length === 0) return
    status = next
    everLoaded = true
    // A set is in flight: keep showing the requested mode until the buds
    // confirm it, so the buttons don't flash back to the old state.
    if (pendingAnc !== "" && String(next.anc || "") === pendingAnc) pendingAnc = ""
    if (opened && !cursorActive) ancIndex = Model.ancIndex(root.anc)
    if (pendingCtl !== null) { dispatchPending(); return }
    if (controlsQueued) { controlsQueued = false; refreshControls() }
  }

  // The plugin never installs anything and never elevates: this only puts
  // the documented install command on the clipboard for the user to run in
  // their own terminal.
  property bool installCmdCopied: false
  function copyInstallCommand() {
    Quickshell.execDetached(["wl-copy", "omarchy pkg aur add pbpctrl"])
    installCmdCopied = true
    copiedReset.restart()
  }
  Timer { id: copiedReset; interval: 4000; onTriggered: root.installCmdCopied = false }

  function setAnc(mode) {
    if (!connected || missingPbpctrl || actionProc.running) return
    if (Model.ANC_MODES.indexOf(mode) < 0) return
    pendingAnc = mode
    actionProc.command = ["sh", "-c",
      'exec timeout 6 pbpctrl -d "$1" set anc "$2"',
      "sh", String(status.addr || ""), mode]
    actionProc.running = true
  }

  function cycleAnc(delta) {
    var i = Model.ancIndex(root.anc)
    var n = Model.ANC_MODES.length
    setAnc(Model.ANC_MODES[((i + delta) % n + n) % n])
  }

  function moveAncCursor(delta) {
    var n = Model.ANC_MODES.length
    ancIndex = ((ancIndex + delta) % n + n) % n
  }


  function tooltip() {
    if (!connected) return "Pixel Buds — not connected"
    if (missingPbpctrl) return budsName + " — pbpctrl is not installed"
    var parts = []
    if (leftPct >= 0) parts.push("L " + leftPct + "%")
    if (rightPct >= 0) parts.push("R " + rightPct + "%")
    if (casePct >= 0) parts.push("Case " + casePct + "%")
    else if (caseStale) parts.push("Case " + caseLastPct + "% (" + Model.ageText(caseLastAge) + ")")
    return budsName + " — " + parts.join(" · ") + " — " + Model.ancLabel(anc)
  }

  IpcHandler {
    target: "douper.pixelbuds"

    function open() { root.open() }
    function close() { root.close() }
    function show() { root.open() }
    function hide() { root.close() }
    function toggle() { root.toggle() }
    function refresh() { root.refresh() }
    function cycleAnc() { root.cycleAnc(1) }
    function setAnc(mode: string) { root.setAnc(mode) }
  }

  Process {
    id: statusProc
    command: ["sh", root.scriptPath]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyStatus(text) }
  }

  Process {
    id: actionProc
    onExited: function(code) {
      if (code !== 0) root.pendingAnc = ""
      root.refresh()
    }
  }

  Process {
    id: controlsProc
    command: ["sh", root.scriptPath, "--controls"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyControls(text) }
  }

  // Whatever a set did (or failed to do), re-read the truth from the buds —
  // unless another set is already waiting, in which case it goes first.
  Process {
    id: ctlProc
    onExited: {
      if (root.pendingCtl !== null) { root.dispatchPending(); return }
      root.refreshControls()
    }
  }

  // Connect/disconnect is event-driven: a plain signal subscription on the
  // system bus (no BecomeMonitor, so no privileges needed). Any device's
  // Connected/ServicesResolved flip triggers a refresh — status.sh is one
  // cheap bluetoothctl call when it isn't the buds.
  Process {
    id: bluezMonitor
    command: ["gdbus", "monitor", "--system", "--dest", "org.bluez"]
    running: true
    stdout: SplitParser {
      onRead: function(line) {
        if (line.indexOf("'Connected'") >= 0 || line.indexOf("'ServicesResolved'") >= 0)
          eventDebounce.restart()
      }
    }
    onExited: monitorRestart.start()
  }
  Timer {
    id: monitorRestart
    interval: 3000
    onTriggered: bluezMonitor.running = true
  }
  Timer {
    id: eventDebounce
    interval: 400
    onTriggered: { root.refresh(); rfcommFollowUp.restart() }
  }
  // The buds' RFCOMM channel isn't up the instant BlueZ says Connected, so the
  // first status pass often has no battery/ANC; one delayed pass fills it in.
  Timer {
    id: rfcommFollowUp
    interval: 4000
    onTriggered: root.refresh()
  }

  // Battery/ANC only change while connected, so the poll runs only then
  // (fast while the panel is open). Disconnected idles at zero cost —
  // reconnects arrive via the BlueZ monitor above.
  Timer {
    interval: root.opened ? 5000 : root.pollInterval
    running: root.connected || root.opened
    repeat: true
    onTriggered: root.refresh()
  }

  Component.onCompleted: refresh()

  onOpenedChanged: {
    if (opened) {
      if (!connected) { close(); return }
      refresh()
      refreshControls()
      ancIndex = Model.ancIndex(root.anc)
      cursorActive = false
      advancedOpen = false
    }
  }
  onConnectedChanged: {
    if (!connected) { close(); controls = {} }
  }

  readonly property bool shown: connected || !hideWhenDisconnected
  visible: shown
  implicitWidth: shown ? button.implicitWidth : 0
  implicitHeight: shown ? button.implicitHeight : 0

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: caseIcon
    active: root.missingPbpctrl
    opacity: root.connected ? 1 : 0.45
    tooltipText: root.tooltip()
    onPressed: function(b) {
      if (!root.connected) return
      if (b === Qt.RightButton || b === Qt.MiddleButton) root.cycleAnc(1)
      else root.toggle()
    }
  }

  // No glyph exists for the closed Pixel Buds case, so draw it: rounded lid
  // and body separated by a thin gap for the hinge seam.
  Component {
    id: caseIcon
    Item {
      id: icon
      readonly property color tint: button.active && button.useActiveColor ? button.activeColor : button.foreground
      onTintChanged: caseCanvas.requestPaint()

      Canvas {
        id: caseCanvas
        anchors.fill: parent
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
          var ctx = getContext("2d")
          ctx.reset()
          // Real case: 2 1/2" tall by 1 7/8" wide, hinge seam 3/4" from the
          // top, flaring out for about another 1/4" below it before the sides
          // go flat. The same curve caps the top and bottom (superellipse
          // quadrants, n = 2.5, each reaching 1" in from its end); only the
          // middle 1/2" is straight side.
          var ch = height * 0.82
          var cw = ch * (1.875 / 2.5)
          var capH = ch * (1.0 / 2.5)
          var cx = width / 2
          // The measured shape still reads a touch tall at bar size: take one
          // pixel row out of the flat section below the hinge.
          var trim = 1
          var top = (height - ch + trim) / 2
          var a = cw / 2
          var n = 2.5
          var yTop = top + capH
          var yBot = top + ch - capH - trim
          ctx.fillStyle = icon.tint
          ctx.beginPath()
          var STEPS = 24
          for (var i = 0; i <= STEPS; i++) {  // top cap, right edge to left
            var t = Math.PI * i / STEPS
            var c = Math.cos(t), s = Math.sin(t)
            var px = cx + a * Math.sign(c) * Math.pow(Math.abs(c), 2 / n)
            var py = yTop - capH * Math.pow(Math.abs(s), 2 / n)
            if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py)
          }
          ctx.lineTo(cx - a, yBot)            // left flat side
          for (var j = 0; j <= STEPS; j++) {  // bottom cap, left edge to right
            var u = Math.PI * j / STEPS
            var cu = Math.cos(u), su = Math.sin(u)
            ctx.lineTo(cx - a * Math.sign(cu) * Math.pow(Math.abs(cu), 2 / n),
                       yBot + capH * Math.pow(Math.abs(su), 2 / n))
          }
          ctx.closePath()                     // right flat side
          ctx.fill()
          var seam = Math.max(1.1, height * 0.06)
          ctx.clearRect(0, top + ch * (0.75 / 2.5) - seam / 2, width, seam)
        }
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened && root.connected
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveAncCursor(dx !== 0 ? dx : dy)
      }
      onActivateRequested: if (root.cursorActive) root.setAnc(Model.ANC_MODES[root.ancIndex])
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroPercent.implicitHeight)

          Text {
            id: heroIcon
            text: Model.ancIcon(root.anc)
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: heroPercent.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              text: root.budsName
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              text: (root.missingPbpctrl ? "pbpctrl is not installed"
                  : root.pendingAnc !== "" ? "Switching to " + Model.ancLabel(root.anc)
                  : Model.ancLabel(root.anc)).toUpperCase()
              color: root.missingPbpctrl ? (root.bar ? root.bar.urgent : Color.urgent) : Qt.darker(root.fg, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }

          Text {
            id: heroPercent
            text: root.minPct >= 0 ? root.minPct + "%" : "—"
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.displayLarge
            font.bold: true
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        // ---------- Missing dependency ----------
        Column {
          visible: root.missingPbpctrl
          width: parent.width
          spacing: Style.space(10)

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Battery levels and listening-mode control come from the pbpctrl CLI, which is packaged in the AUR. Run in a terminal:"
            color: Qt.darker(root.fg, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            width: parent.width
            text: "omarchy pkg aur add pbpctrl"
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Button {
            width: parent.width
            text: root.installCmdCopied ? "Copied — paste it in a terminal" : "Copy the install command"
            fontSize: Style.font.bodySmall
            foreground: root.fg
            fontFamily: root.fontFamily
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
            bordered: true
            active: root.installCmdCopied
            onClicked: root.copyInstallCommand()
          }
        }

        // ---------- Batteries ----------
        Column {
          visible: !root.missingPbpctrl
          width: parent.width
          spacing: Style.space(8)

          BatteryRow {
            label: "Left"
            level: root.leftPct
            charging: root.leftCharging
            hint: root.leftInCase ? "in case" : ""
          }
          BatteryRow {
            label: "Right"
            level: root.rightPct
            charging: root.rightCharging
            hint: root.rightInCase ? "in case" : ""
          }
          BatteryRow {
            label: "Case"
            level: root.casePct >= 0 ? root.casePct : root.caseLastPct
            charging: root.caseCharging
            stale: root.caseStale
            hint: root.caseStale ? "last seen " + Model.ageText(root.caseLastAge)
                : root.casePct < 0 ? "reports when a bud is docked" : ""
          }
        }

        // ---------- Listening mode ----------
        PanelSeparator { visible: !root.missingPbpctrl; foreground: root.fg }

        Column {
          visible: !root.missingPbpctrl
          width: parent.width
          spacing: Style.space(10)

          PanelSectionHeader {
            text: "LISTENING MODE"
            foreground: root.fg
            fontFamily: root.fontFamily
          }

          Row {
            id: modeRow
            width: parent.width
            spacing: Style.space(6)
            readonly property real cellWidth: (width - spacing * (Model.ANC_MODES.length - 1)) / Model.ANC_MODES.length

            Repeater {
              model: Model.ANC_MODES
              Button {
                required property var modelData
                required property int index
                width: modeRow.cellWidth
                iconText: Model.ancIcon(String(modelData))
                iconSize: Style.font.title
                text: Model.ancShort(String(modelData))
                fontSize: Style.font.bodySmall
                foreground: root.fg
                fontFamily: root.fontFamily
                horizontalPadding: Style.spacing.controlPaddingX
                verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
                bordered: true
                active: root.anc === String(modelData)
                hasCursor: root.cursorActive && root.ancIndex === index
                tooltipText: Model.ancLabel(String(modelData))
                onClicked: root.setAnc(String(modelData))
                onHovered: function(h) {
                  if (h) { root.cursorActive = true; root.ancIndex = index }
                }
              }
            }
          }
        }

        // ---------- Advanced (collapsed): device toggles + sound ----------
        PanelSeparator { visible: advancedHeader.visible; foreground: root.fg }

        Item {
          id: advancedHeader
          visible: !root.missingPbpctrl
          width: parent.width
          implicitHeight: advLabel.implicitHeight + Style.space(4)

          Text {
            id: advLabel
            text: (root.advancedOpen ? "▾" : "▸") + "  ADVANCED"
            color: Qt.darker(root.fg, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 1.2
            anchors.verticalCenter: parent.verticalCenter
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: {
              root.advancedOpen = !root.advancedOpen
              if (root.advancedOpen && Object.keys(root.controls).length === 0) root.refreshControls()
            }
          }
        }

        Column {
          visible: root.advancedOpen && advancedHeader.visible
          width: parent.width
          spacing: Style.space(6)

          Text {
            visible: Object.keys(root.controls).length === 0
            width: parent.width
            text: controlsProc.running ? "Reading device settings…" : "The buds reported no adjustable settings."
            color: Qt.darker(root.fg, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          ToggleRow { label: "Multipoint audio"; ctlKey: "multipoint"; statusKey: "ctl_multipoint" }
          ToggleRow { label: "Speech detection"; ctlKey: "speech-detection"; statusKey: "ctl_speech_detection" }
          ToggleRow { label: "On-head detection"; ctlKey: "ohd"; statusKey: "ctl_ohd" }
          ToggleRow { label: "Volume level alerts"; ctlKey: "volume-exposure-notifications"; statusKey: "ctl_volume_exposure_notifications" }

          PanelSectionHeader {
            visible: root.controls.ctl_volume_eq !== undefined
                || root.controls.ctl_mono !== undefined
                || root.controls.ctl_balance !== undefined
                || root.controls.ctl_eq !== undefined
            text: "SOUND"
            foreground: root.fg
            fontFamily: root.fontFamily
          }

          ToggleRow { label: "Volume EQ"; ctlKey: "volume-eq"; statusKey: "ctl_volume_eq" }
          ToggleRow { label: "Mono audio"; ctlKey: "mono"; statusKey: "ctl_mono" }

          SliderRow {
            label: "Balance"
            visible: root.controls.ctl_balance !== undefined
            from: -100; to: 100; step: 5
            value: parseInt(root.controls.ctl_balance) || 0
            format: function(v) { return v === 0 ? "center" : (v < 0 ? "L " + (-v) : "R " + v) }
            onCommitted: function(v) { root.setControl("balance", String(v)) }
          }

          Repeater {
            model: ["Low bass", "Bass", "Mid", "Treble", "Upper treble"]
            SliderRow {
              required property var modelData
              required property int index
              label: modelData
              visible: root.eqBands.length === 5
              from: -6; to: 6; step: 0.5
              value: root.eqBands.length === 5 ? root.eqBands[index] : 0
              format: function(v) { return v.toFixed(1) }
              onCommitted: function(v) { root.setEqBand(index, v) }
            }
          }
        }

        Text {
          visible: !!root.status.error && !root.missingPbpctrl
          width: parent.width
          wrapMode: Text.WordWrap
          text: String(root.status.error || "")
          color: root.bar ? root.bar.urgent : Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }

  component BatteryRow: Item {
    id: row
    property string label: ""
    property int level: -1
    property bool charging: false
    property bool stale: false
    property string hint: ""

    readonly property bool low: level >= 0 && level <= 20 && !charging

    width: parent.width
    implicitHeight: labelText.implicitHeight + track.height + Style.space(6)

    Text {
      id: labelText
      anchors.left: parent.left
      anchors.top: parent.top
      text: row.label + (row.hint !== "" ? "  ·  " + row.hint : "")
      color: root.fg
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {
      anchors.right: parent.right
      anchors.top: parent.top
      text: (row.level >= 0 ? row.level + "%" : "—") + "  " + Model.batteryIcon(row.level, row.charging)
      color: row.low ? root.urgentColor : root.fg
      opacity: row.stale ? 0.5 : 1.0
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Rectangle {
      id: track
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      height: Style.space(6)
      radius: height / 2
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)

      Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        radius: parent.radius
        color: row.low ? root.urgentColor : root.fg
        opacity: row.stale ? 0.4 : 1.0
        width: row.level >= 0 ? Math.max(parent.height, parent.width * row.level / 100) : 0
        Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }

        SequentialAnimation on opacity {
          running: row.charging && root.opened
          loops: Animation.Infinite
          alwaysRunToEnd: true
          NumberAnimation { from: 1.0; to: 0.55; duration: 950; easing.type: Easing.InOutSine }
          NumberAnimation { from: 0.55; to: 1.0; duration: 950; easing.type: Easing.InOutSine }
        }
      }
    }
  }

  // A label with an On/Off button, shown only when the buds reported a state
  // for it — an unanswered control renders nothing at all.
  component ToggleRow: Item {
    id: trow
    property string label: ""
    property string ctlKey: ""
    property string statusKey: ""
    readonly property bool known: root.controls[statusKey] !== undefined
    readonly property bool on: String(root.controls[statusKey] || "false") === "true"

    visible: known
    width: parent.width
    implicitHeight: known ? toggleBtn.implicitHeight : 0

    Text {
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: trow.label
      color: root.fg
      opacity: 0.8
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Button {
      id: toggleBtn
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: trow.on ? "On" : "Off"
      fontSize: Style.font.bodySmall
      foreground: root.fg
      fontFamily: root.fontFamily
      horizontalPadding: Style.spacing.controlPaddingX
      verticalPadding: Style.spacing.controlPaddingY
      bordered: true
      active: trow.on
      enabled: !ctlProc.running
      opacity: enabled ? 1.0 : 0.5
      onClicked: root.setControl(trow.ctlKey, trow.on ? "false" : "true")
    }
  }

  // Drag-to-set slider; the value is committed to the buds on release and
  // then re-read, so the row always ends up showing the device's truth.
  component SliderRow: Item {
    id: srow
    property string label: ""
    property real from: 0
    property real to: 100
    property real step: 1
    property real value: 0
    property var format: function(v) { return String(Math.round(v)) }
    signal committed(real v)

    property bool dragging: false
    property real dragVal: 0
    // Between release and the device answering, keep showing what was asked
    // for; the next controls refresh replaces it with the device's truth.
    property bool holding: false
    property real holdVal: 0
    readonly property real shownVal: dragging ? dragVal : (holding ? holdVal : value)

    Connections {
      target: root
      function onControlsChanged() { srow.holding = false }
    }

    width: parent.width
    implicitHeight: sliderLabel.implicitHeight + strack.height + Style.space(6)

    function valueAt(x) {
      var t = Math.max(0, Math.min(1, x / strack.width))
      var v = from + t * (to - from)
      v = Math.round(v / step) * step
      return Math.max(from, Math.min(to, v))
    }

    Text {
      id: sliderLabel
      anchors.left: parent.left
      anchors.top: parent.top
      text: srow.label
      color: root.fg
      opacity: 0.8
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {
      anchors.right: parent.right
      anchors.top: parent.top
      text: srow.format(srow.shownVal)
      color: root.fg
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Rectangle {
      id: strack
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      height: Style.space(6)
      radius: height / 2
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)

      Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        radius: parent.radius
        color: root.fg
        width: Math.max(parent.height, parent.width * (srow.shownVal - srow.from) / (srow.to - srow.from))
      }

      MouseArea {
        anchors.fill: parent
        anchors.margins: -Style.space(6)
        enabled: !ctlProc.running
        onPressed: function(mouse) { srow.dragging = true; srow.dragVal = srow.valueAt(mouse.x + Style.space(6)) }
        onPositionChanged: function(mouse) { if (srow.dragging) srow.dragVal = srow.valueAt(mouse.x + Style.space(6)) }
        onReleased: {
          if (!srow.dragging) return
          srow.dragging = false
          if (srow.dragVal !== srow.value) {
            srow.holding = true
            srow.holdVal = srow.dragVal
            srow.committed(srow.dragVal)
          }
        }
      }
    }
  }
}
