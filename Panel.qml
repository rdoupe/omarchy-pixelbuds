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
  property bool everLoaded: false
  property int ancIndex: 1
  property bool cursorActive: false
  property string pendingAnc: ""

  readonly property string scriptPath: String(Qt.resolvedUrl("status.sh")).replace(/^file:\/\//, "")
  readonly property int pollInterval: Math.max(5, parseInt(setting("pollIntervalSec", 30)) || 30) * 1000

  readonly property bool connected: String(status.connected || "0") === "1"
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

  function refresh() {
    if (statusProc.running) return
    statusProc.running = true
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
  }

  function setAnc(mode) {
    if (!connected || actionProc.running) return
    if (Model.ANC_MODES.indexOf(mode) < 0) return
    pendingAnc = mode
    actionProc.command = ["sh", "-c",
      'PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; exec timeout 6 pbpctrl -d "$1" set anc "$2"',
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
    if (!connected) return ""
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
      ancIndex = Model.ancIndex(root.anc)
      cursorActive = false
    }
  }
  onConnectedChanged: if (!connected) close()

  visible: connected
  implicitWidth: connected ? button.implicitWidth : 0
  implicitHeight: connected ? button.implicitHeight : 0

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: caseIcon
    active: false
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
          var top = (height - ch) / 2
          var a = cw / 2
          var n = 2.5
          var yTop = top + capH
          var yBot = top + ch - capH
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
              text: (root.pendingAnc !== "" ? "Switching to " + Model.ancLabel(root.anc) : Model.ancLabel(root.anc)).toUpperCase()
              color: Qt.darker(root.fg, 1.4)
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

        // ---------- Batteries ----------
        Column {
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
        PanelSeparator { foreground: root.fg }

        Column {
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

        Text {
          visible: !!root.status.error
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
      color: root.fg
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
        color: root.fg
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
}
